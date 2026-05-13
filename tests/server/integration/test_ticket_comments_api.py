"""v0.10.1 Phase 4 — ticket comments + compiled context regression tests.

Covers the comments routes (GET + POST) and the extended start_ticket
envelope (TicketResponse + compiled_context). The persona context
compilation helper is exercised end-to-end through the start route —
no separate test for the helper since it's only consumed by start.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    KnowledgeEntry,
    Project,
    Ticket,
    TicketDependency,
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
        name=f"phase4-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/p4-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas",
    content: str = "# Atlas\n\nBackend persona content.",
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend Architect",
        content=content,
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


# ── Comments routes ────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_comment_returns_201_and_persists(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "comment-test"},
    )
    tk_id = tk.json()["id"]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "Progress update: investigating..."},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ticket_id"] == tk_id
    assert body["author_user_id"] == user.id
    assert body["content"] == "Progress update: investigating..."
    assert body["author_persona"] is None
    assert body["session_id"] is None
    assert body["id"].startswith("tc_")


@pytest.mark.asyncio
async def test_list_comments_returns_chronological(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "comment-list-test"},
    )
    tk_id = tk.json()["id"]
    for msg in ("first", "second", "third"):
        await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
            headers=_hdrs(key),
            json={"content": msg},
        )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["content"] for c in body] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_comment_with_persona_attribution(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "persona-comment-test"},
    )
    tk_id = tk.json()["id"]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "Speaking as atlas", "author_persona": "atlas"},
    )
    assert resp.status_code == 201
    assert resp.json()["author_persona"] == "atlas"


@pytest.mark.asyncio
async def test_empty_comment_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "empty-test"},
    )
    tk_id = tk.json()["id"]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "   "},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_comment_on_missing_ticket_returns_404(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/tk_missing/comments",
        headers=_hdrs(key),
        json={"content": "hello"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tier_gating_on_comments(
    client: AsyncClient, db_session: AsyncSession
):
    """Comments inherit agent_tickets tier gate (TEAM+)."""
    user, key = await _make_user(db_session, tier="pro")
    project = await _make_project(db_session, user)
    # Even reading the empty list fails on Pro.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/tk_x/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 403


# ── start_ticket compiled context ──────────────────────────


@pytest.mark.asyncio
async def test_start_returns_compiled_context_with_persona(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(
        db_session, project, user, "atlas",
        content="Backend architect with strong opinions about types.",
    )
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Fix the rate limiter",
            "description": "The KB search endpoint allows unbounded queries.",
            "assigned_to": "atlas",
            "priority": "high",
            "acceptance_criteria": ["Per-user rate limit", "Tier-aware"],
            "file_refs": ["src/sessionfs/server/routes/knowledge.py"],
        },
    )
    tk_id = tk.json()["id"]
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200
    body = start.json()
    assert body["ticket"]["status"] == "in_progress"
    ctx = body["compiled_context"]
    # Persona section.
    assert "You are atlas — Backend Architect" in ctx
    assert "strong opinions about types" in ctx
    # Ticket section.
    assert "Fix the rate limiter" in ctx
    assert "Priority: high" in ctx
    # Acceptance criteria as checkboxes.
    assert "- [ ] Per-user rate limit" in ctx
    assert "- [ ] Tier-aware" in ctx
    # File refs as bullets.
    assert "src/sessionfs/server/routes/knowledge.py" in ctx


@pytest.mark.asyncio
async def test_start_context_omits_persona_when_unassigned(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "unassigned"},
    )
    tk_id = tk.json()["id"]
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200
    ctx = start.json()["compiled_context"]
    # Persona header should NOT be present.
    assert "You are " not in ctx
    # Ticket section IS present.
    assert "Current Ticket: unassigned" in ctx


@pytest.mark.asyncio
async def test_start_context_includes_recent_comments(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "comments-in-context", "assigned_to": "atlas"},
    )
    tk_id = tk.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "Spotted a related bug in the cache layer."},
    )
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    ctx = start.json()["compiled_context"]
    assert "Recent Comments" in ctx
    assert "Spotted a related bug" in ctx


@pytest.mark.asyncio
async def test_start_context_tool_specific_truncation(
    client: AsyncClient, db_session: AsyncSession
):
    """The `tool` query param sizes context to the target tool's budget.

    For a tiny content under the threshold, both `claude-code` (16k)
    and `cursor` (4k) return the same content; for a content that
    exceeds cursor's budget but fits claude-code's, only cursor adds
    the truncation marker.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Long content that easily exceeds cursor's 4k * 4 = 16k char budget.
    huge_content = "## section\n" + ("- detail\n" * 5000)
    await _make_persona(db_session, project, user, "atlas", content=huge_content)
    # Two independent tickets so each `start` is the first transition
    # — `start` is once-per-ticket atomic.
    tk_cursor = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "trunc-cursor", "assigned_to": "atlas"},
    )
    tk_claude = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "trunc-claude", "assigned_to": "atlas"},
    )

    cursor = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_cursor.json()['id']}/start"
        "?tool=cursor",
        headers=_hdrs(key),
    )
    claude = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_claude.json()['id']}/start"
        "?tool=claude-code",
        headers=_hdrs(key),
    )
    cursor_ctx = cursor.json()["compiled_context"]
    claude_ctx = claude.json()["compiled_context"]
    # cursor (4k tokens = 16k chars) truncates the huge content.
    assert "[...truncated to fit tool token limit]" in cursor_ctx
    # Claude-code (16k tokens = 64k chars) fits the content.
    assert len(claude_ctx) > len(cursor_ctx)


# ── Cross-project KB leak guard (KB 332 HIGH fix) ──────────


@pytest.mark.asyncio
async def test_start_context_does_not_leak_cross_project_kb_claims(
    client: AsyncClient, db_session: AsyncSession
):
    """A ticket in project B with context_refs=[claim_a.id] from project A
    must not include claim A content in compiled_context.
    """
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)

    # Plant a claim-class KB entry in project A.
    secret_content = "PROJECT_A_SECRET_CLAIM_CONTENT_xyz789"
    claim_a = KnowledgeEntry(
        project_id=project_a.id,
        session_id="manual",
        user_id=user.id,
        entry_type="decision",
        content=secret_content,
        claim_class="claim",
        freshness_class="current",
        dismissed=False,
    )
    db_session.add(claim_a)
    await db_session.commit()
    await db_session.refresh(claim_a)

    # Create a ticket in project B that references project A's claim id.
    tk = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "cross-project leak attempt",
            "context_refs": [str(claim_a.id)],
        },
    )
    tk_id = tk.json()["id"]

    start = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200
    ctx = start.json()["compiled_context"]
    # The foreign claim content MUST NOT appear in project B's context.
    assert secret_content not in ctx
    assert "Relevant Project Knowledge" not in ctx


@pytest.mark.asyncio
async def test_start_context_omits_dismissed_and_superseded_claims(
    client: AsyncClient, db_session: AsyncSession
):
    """Compiled context must drop dismissed claims and superseded claims
    even when they're hand-picked via context_refs.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    dismissed_claim = KnowledgeEntry(
        project_id=project.id,
        session_id="manual",
        user_id=user.id,
        entry_type="decision",
        content="DISMISSED_CLAIM_MARKER_abc",
        claim_class="claim",
        freshness_class="current",
        dismissed=True,
    )
    fresh_claim = KnowledgeEntry(
        project_id=project.id,
        session_id="manual",
        user_id=user.id,
        entry_type="decision",
        content="FRESH_CLAIM_MARKER_def",
        claim_class="claim",
        freshness_class="current",
        dismissed=False,
    )
    db_session.add_all([dismissed_claim, fresh_claim])
    await db_session.commit()
    await db_session.refresh(dismissed_claim)
    await db_session.refresh(fresh_claim)

    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "dismissed-and-fresh",
            "context_refs": [str(dismissed_claim.id), str(fresh_claim.id)],
        },
    )
    tk_id = tk.json()["id"]
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    ctx = start.json()["compiled_context"]
    assert "FRESH_CLAIM_MARKER_def" in ctx
    assert "DISMISSED_CLAIM_MARKER_abc" not in ctx


@pytest.mark.asyncio
async def test_start_context_does_not_leak_cross_project_dep_completion_notes(
    client: AsyncClient, db_session: AsyncSession
):
    """KB 334 MEDIUM: a stale cross-project TicketDependency must not
    leak a foreign project's completion_notes through compiled_context.
    The normal create path now rejects cross-project deps, so we plant
    the bad row directly (simulating a legacy bug or manual DB edit).
    """
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)

    # Plant a done ticket with secret completion notes in project A.
    secret = "PROJECT_A_DEP_COMPLETION_NOTES_SECRET_abc999"
    done_a = Ticket(
        id=f"tk_a_{uuid.uuid4().hex[:8]}",
        project_id=project_a.id,
        title="cross-project dep source",
        description="",
        priority="medium",
        created_by_user_id=user.id,
        status="done",
        completion_notes=secret,
    )
    db_session.add(done_a)
    await db_session.commit()

    # Create a normal ticket in project B (via API so all defaults land).
    tk_b_resp = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets",
        headers=_hdrs(key),
        json={"title": "victim ticket"},
    )
    tk_b_id = tk_b_resp.json()["id"]

    # Plant a malicious TicketDependency directly (bypasses the API
    # same-project validation) — this is the legacy bad-edge scenario.
    db_session.add(TicketDependency(ticket_id=tk_b_id, depends_on_id=done_a.id))
    await db_session.commit()

    start = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets/{tk_b_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200
    ctx = start.json()["compiled_context"]
    assert secret not in ctx
    assert "From completed dependency" not in ctx
