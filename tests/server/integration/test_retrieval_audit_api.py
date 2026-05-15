"""Server-side retrieval audit API regression tests.

Enterprise SoD needs a server-authoritative record of which MCP
retrievals shaped an agent run. Local JSONL remains a fallback, but
these routes are the durable API path.
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
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    Project,
    Session,
    Ticket,
    User,
)


async def _make_user(
    db: AsyncSession, name: str = "alice", tier: str = "team"
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


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"retrieval-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/retrieval-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(
    db: AsyncSession, project: Project, owner: User, ticket_id: str | None = None
) -> Ticket:
    ticket = Ticket(
        id=ticket_id or f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="audit-test-ticket",
        description="x",
        status="open",
        priority="medium",
        created_by_user_id=owner.id,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend Architect",
        content="# Atlas\n\nTest persona",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


def _session_archive(project: Project, retrieval_audit_id: str) -> bytes:
    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_claimedraid123",
        "title": "claimed raid",
        "retrieval_audit_id": retrieval_audit_id,
        "source": {"tool": "codex"},
        "stats": {"message_count": 1},
    }
    workspace = {
        "git": {
            "remote_url": f"https://github.com/{project.git_remote_normalized}.git",
            "branch": "main",
            "commit_sha": "a" * 40,
        }
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("manifest.json", json.dumps(manifest).encode()),
            ("workspace.json", json.dumps(workspace).encode()),
            ("messages.jsonl", b'{"role":"user","content":"hello"}\n'),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_retrieval_audit_context_event_and_session_log(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project, user, ticket_id="tk_123")
    await _make_persona(db_session, project, user, name="atlas")

    ctx_resp = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"ticket_id": ticket.id, "persona_name": "atlas", "lease_epoch": 7},
    )
    assert ctx_resp.status_code == 201, ctx_resp.text
    context_id = ctx_resp.json()["id"]
    assert context_id.startswith("ra_")

    event_resp = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-events",
        headers=_hdrs(key),
        json={
            "context_id": context_id,
            "session_id": "ses_audit",
            "tool_name": "get_context_section",
            "arguments": {"slug": "architecture"},
            "returned_refs": {"slugs": ["architecture"], "kb_entry_ids": ["42"]},
        },
    )
    assert event_resp.status_code == 201, event_resp.text
    assert event_resp.json()["caller_user_id"] == user.id

    huge_event = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-events",
        headers=_hdrs(key),
        json={
            "context_id": context_id,
            "tool_name": "get_wiki_page",
            "arguments": {"slug": "architecture", "blob": "x" * 20000},
            "returned_refs": {"slugs": ["architecture"], "blob": ["x" * 20000]},
        },
    )
    assert huge_event.status_code == 201, huge_event.text
    assert huge_event.json()["arguments"]["_truncated"] is True
    assert huge_event.json()["returned_refs"]["_truncated"] is True

    context_log = await client.get(
        f"/api/v1/retrieval-audit-contexts/{context_id}/events",
        headers=_hdrs(key),
    )
    assert context_log.status_code == 200, context_log.text
    assert context_log.json()["count"] == 2
    row = context_log.json()["events"][0]
    assert row["tool_name"] == "get_context_section"
    assert row["arguments"]["slug"] == "architecture"
    assert row["returned_refs"]["kb_entry_ids"] == ["42"]

    db_session.add(
        Session(
            id="ses_audit",
            user_id=user.id,
            project_id=project.id,
            source_tool="codex",
            blob_key="sessions/ses_audit.sfs",
            blob_size_bytes=1,
            etag="etag",
            messages_text="",
            retrieval_audit_id=context_id,
        )
    )
    await db_session.commit()

    session_log = await client.get(
        "/api/v1/sessions/ses_audit/retrieval-log",
        headers=_hdrs(key),
    )
    assert session_log.status_code == 200, session_log.text
    assert session_log.json()["retrieval_audit_id"] == context_id
    assert session_log.json()["count"] == 2


@pytest.mark.asyncio
async def test_retrieval_audit_event_rejects_cross_project_context(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    ticket_a = await _make_ticket(db_session, project_a, user)
    await _make_persona(db_session, project_a, user, name="atlas")

    ctx_resp = await client.post(
        f"/api/v1/projects/{project_a.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"ticket_id": ticket_a.id, "persona_name": "atlas"},
    )
    assert ctx_resp.status_code == 201, ctx_resp.text
    context_id = ctx_resp.json()["id"]

    event_resp = await client.post(
        f"/api/v1/projects/{project_b.id}/retrieval-audit-events",
        headers=_hdrs(key),
        json={
            "context_id": context_id,
            "tool_name": "get_persona",
            "arguments": {"name": "atlas"},
        },
    )
    assert event_resp.status_code == 404


@pytest.mark.asyncio
async def test_session_upload_drops_retrieval_audit_id_owned_by_another_user(
    client: AsyncClient, db_session: AsyncSession
):
    alice, alice_key = await _make_user(db_session, "alice")
    bob, bob_key = await _make_user(db_session, "bob")
    alice_project = await _make_project(db_session, alice)
    bob_project = await _make_project(db_session, bob)
    alice_ticket = await _make_ticket(db_session, alice_project, alice)
    await _make_persona(db_session, alice_project, alice, name="atlas")

    ctx_resp = await client.post(
        f"/api/v1/projects/{alice_project.id}/retrieval-audit-contexts",
        headers=_hdrs(alice_key),
        json={"ticket_id": alice_ticket.id, "persona_name": "atlas"},
    )
    assert ctx_resp.status_code == 201, ctx_resp.text
    alice_context_id = ctx_resp.json()["id"]

    archive = _session_archive(bob_project, alice_context_id)
    upload = await client.put(
        "/api/v1/sessions/ses_claimedraid123/sync",
        headers=_hdrs(bob_key),
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert upload.status_code == 201, upload.text

    stored = (
        await db_session.execute(
            select(Session).where(Session.id == "ses_claimedraid123")
        )
    ).scalar_one()
    assert stored.user_id == bob.id
    assert stored.project_id == bob_project.id
    assert stored.retrieval_audit_id is None


# ─── tk_b3ee62c732c44594 Finding A: context-create validates links ───


@pytest.mark.asyncio
async def test_create_context_rejects_nonexistent_ticket_id(
    client: AsyncClient, db_session: AsyncSession
):
    """Caller passes a ticket_id that doesn't exist anywhere → 422."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"ticket_id": "tk_does_not_exist"},
    )
    assert resp.status_code == 422
    assert "ticket_id" in resp.text


@pytest.mark.asyncio
async def test_create_context_rejects_cross_project_ticket_id(
    client: AsyncClient, db_session: AsyncSession
):
    """ticket_id from project B refused when called against project A."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    foreign = await _make_ticket(db_session, project_b, user)

    resp = await client.post(
        f"/api/v1/projects/{project_a.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"ticket_id": foreign.id},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_context_rejects_nonexistent_persona_name(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"persona_name": "ghost"},
    )
    assert resp.status_code == 422
    assert "persona_name" in resp.text


@pytest.mark.asyncio
async def test_create_context_rejects_cross_project_persona_name(
    client: AsyncClient, db_session: AsyncSession
):
    """persona_name that only exists in project B refused for project A."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    await _make_persona(db_session, project_b, user, name="sentinel")

    resp = await client.post(
        f"/api/v1/projects/{project_a.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={"persona_name": "sentinel"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_context_accepts_null_link_fields(
    client: AsyncClient, db_session: AsyncSession
):
    """No ticket_id and no persona_name is a valid bare context."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts",
        headers=_hdrs(key),
        json={},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticket_id"] is None
    assert body["persona_name"] is None
