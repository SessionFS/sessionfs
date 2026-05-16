"""Integration tests for wiki pages, entry creation, and auto_narrative."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    KnowledgePage,
    Project,
    User,
)


@pytest.fixture
async def test_project(db_session: AsyncSession, test_user: User) -> Project:
    """Create a test project."""
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Wiki Test Project",
        git_remote_normalized="github.com/example/wiki-repo",
        context_document="# Project Context\n\n## Overview\nTest.\n",
        owner_id=test_user.id,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Entry creation via API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_entry_via_api(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test POST /{project_id}/entries/add creates a knowledge entry."""
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        json={
            "content": "Always use UTC timestamps",
            "entry_type": "convention",
            "confidence": 0.95,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "Always use UTC timestamps"
    assert data["entry_type"] == "convention"
    assert data["confidence"] == 0.7  # capped for session_id="manual"
    assert data["session_id"] == "manual"
    assert data["project_id"] == test_project.id


@pytest.mark.asyncio
async def test_add_entry_invalid_type(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test that invalid entry_type is rejected."""
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        json={"content": "test", "entry_type": "invalid_type"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_entry_with_session_id(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test that session_id is stored when provided."""
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        json={
            "content": "Found a race condition in sync",
            "entry_type": "bug",
            "session_id": "ses_abc123",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["session_id"] == "ses_abc123"


# ---------------------------------------------------------------------------
# Page CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_page(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test PUT creates a new page."""
    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={"content": "# Architecture\n\nMicroservices.", "title": "Architecture"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "architecture"
    assert data["title"] == "Architecture"
    assert data["word_count"] == 3  # "#", "Architecture", "Microservices."
    assert data["content"] == "# Architecture\n\nMicroservices."


@pytest.mark.asyncio
async def test_update_page(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test PUT updates an existing page."""
    # Create
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/conventions",
        json={"content": "Use black.", "title": "Conventions"},
        headers=auth_headers,
    )
    # Update
    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/conventions",
        json={"content": "Use black and ruff for formatting."},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "Use black and ruff for formatting."
    assert data["title"] == "Conventions"  # Title preserved from creation


@pytest.mark.asyncio
async def test_page_revision_history_recorded_per_write(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 — every PUT appends a wiki_page_revisions row, and GET
    /history returns them in revised_at DESC, id DESC order."""
    from sessionfs.server.db.models import AgentPersona, Ticket

    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        name="atlas",
        role="Backend",
        created_by=test_user.id,
    )
    db_session.add(persona)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        title="Wiki history test",
        description="x",
        priority="medium",
        status="in_progress",
        assigned_to="atlas",
        created_by_user_id=test_user.id,
    )
    db_session.add(ticket)
    await db_session.commit()

    # First revision — bare body, no provenance
    r1 = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={"content": "v1 content", "title": "Architecture"},
        headers=auth_headers,
    )
    assert r1.status_code == 200

    # Second revision — same user, with persona + ticket
    r2 = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "v2 content updated",
            "persona_name": "atlas",
            "ticket_id": ticket.id,
        },
        headers=auth_headers,
    )
    assert r2.status_code == 200

    # Third revision — just content
    r3 = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={"content": "v3 content"},
        headers=auth_headers,
    )
    assert r3.status_code == 200

    hist = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/architecture/history",
        headers=auth_headers,
    )
    assert hist.status_code == 200, hist.text
    data = hist.json()
    assert data["slug"] == "architecture"
    assert data["count"] == 3
    revs = data["revisions"]
    # Newest first by (revised_at DESC, id DESC)
    assert [r["revision_number"] for r in revs] == [3, 2, 1]
    # Revision 2 carries the provenance we sent
    assert revs[1]["persona_name"] == "atlas"
    assert revs[1]["ticket_id"] == ticket.id
    # Revisions 1 and 3 have no persona/ticket
    assert revs[0]["persona_name"] is None
    assert revs[2]["persona_name"] is None


@pytest.mark.asyncio
async def test_revision_provenance_accepts_active_executor(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 R3 — a user who STARTED a ticket they didn't create
    has an open RetrievalAuditContext for it. That counts as ownership
    for wiki provenance attribution. Without this, agents executing
    a colleague's ticket can't attribute revisions to it."""
    from sessionfs.server.db.models import RetrievalAuditContext, Ticket

    # Ticket created by SOMEONE ELSE, but test_user has started it
    # (simulated by inserting an open RetrievalAuditContext)
    other_user = User(
        id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Other",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    await db_session.commit()
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        title="Started by test_user",
        description="x",
        priority="medium",
        status="in_progress",
        lease_epoch=1,
        created_by_user_id=other_user.id,
    )
    db_session.add(ticket)
    db_session.add(
        RetrievalAuditContext(
            id=f"ra_{uuid.uuid4().hex[:16]}",
            project_id=test_project.id,
            ticket_id=ticket.id,
            created_by_user_id=test_user.id,
            lease_epoch=1,  # matches ticket
        )
    )
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "attribute as the active executor",
            "ticket_id": ticket.id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_revision_provenance_rejects_stale_lease_executor(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 R4 — executor write rights expire when someone
    force-starts the ticket (ticket.lease_epoch bumps but the user's
    old RetrievalAuditContext keeps its old lease_epoch). The
    validator now rejects this so audit provenance stays tight."""
    from sessionfs.server.db.models import RetrievalAuditContext, Ticket

    other_user = User(
        id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Other",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    await db_session.commit()
    # Ticket has been force-restarted: lease_epoch is now 2
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        title="Force-restarted",
        description="x",
        priority="medium",
        status="in_progress",
        lease_epoch=2,
        created_by_user_id=other_user.id,
    )
    db_session.add(ticket)
    # test_user's audit context is from the OLD lease (epoch 1)
    db_session.add(
        RetrievalAuditContext(
            id=f"ra_{uuid.uuid4().hex[:16]}",
            project_id=test_project.id,
            ticket_id=ticket.id,
            created_by_user_id=test_user.id,
            lease_epoch=1,  # stale
        )
    )
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "stale-lease attribution attempt",
            "ticket_id": ticket.id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revision_provenance_rejects_executor_after_complete(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 R4 — executor write rights expire when the ticket
    moves out of in_progress. After complete/accept/cancel the
    user is no longer the active writer for provenance purposes."""
    from sessionfs.server.db.models import RetrievalAuditContext, Ticket

    other_user = User(
        id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Other",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    await db_session.commit()
    # Ticket has moved to review (post-complete)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        title="Completed elsewhere",
        description="x",
        priority="medium",
        status="review",  # no longer in_progress
        lease_epoch=1,
        created_by_user_id=other_user.id,
    )
    db_session.add(ticket)
    db_session.add(
        RetrievalAuditContext(
            id=f"ra_{uuid.uuid4().hex[:16]}",
            project_id=test_project.id,
            ticket_id=ticket.id,
            created_by_user_id=test_user.id,
            lease_epoch=1,  # matches but status doesn't
        )
    )
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "post-review attribution attempt",
            "ticket_id": ticket.id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revision_provenance_rejects_unowned_ticket(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 R2 — same-project users can't attribute revisions to
    a ticket they don't own (must be created_by_user_id or
    resolver_user_id). Closes Codex R2 MEDIUM finding (provenance
    association)."""
    from sessionfs.server.db.models import Ticket

    # Ticket created by SOMEONE ELSE in this same project
    other_user = User(
        id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Other",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    await db_session.commit()
    foreign_ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        title="Not yours",
        description="x",
        priority="medium",
        status="in_progress",
        created_by_user_id=other_user.id,
    )
    db_session.add(foreign_ticket)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "I attribute to your ticket",
            "ticket_id": foreign_ticket.id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "not owned by you" in resp.text


@pytest.mark.asyncio
async def test_page_history_rejects_foreign_project_persona(
    client: AsyncClient, auth_headers: dict, test_project: Project,
    db_session: AsyncSession, test_user: User,
):
    """v0.10.7 — provenance persona must exist in this project. A
    persona from another project must not be acceptable as the author
    of a revision in this project. Mirrors cb8a9da cross-project
    leak defense."""
    from sessionfs.server.db.models import AgentPersona

    # Persona in a DIFFERENT project
    other = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Other",
        git_remote_normalized=f"acme/other-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=test_user.id,
    )
    db_session.add(other)
    await db_session.commit()
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:16]}",
            project_id=other.id,
            name="ghost",
            role="Backend",
            created_by=test_user.id,
        )
    )
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/pages/architecture",
        json={
            "content": "forged provenance",
            "persona_name": "ghost",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_page_history_pagination_via_cursor(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """v0.10.7 — `cursor=last_id` returns older revisions only."""
    for i in range(5):
        await client.put(
            f"/api/v1/projects/{test_project.id}/pages/paged",
            json={"content": f"rev {i}"},
            headers=auth_headers,
        )

    first = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/paged/history?limit=2",
        headers=auth_headers,
    )
    page1 = first.json()
    assert len(page1["revisions"]) == 2
    assert [r["revision_number"] for r in page1["revisions"]] == [5, 4]
    # v0.10.7 R3 — next_cursor is exposed on the envelope; ids on each
    # revision. Use the envelope cursor for the follow-up request
    # (don't guess insertion-order ids).
    assert page1["next_cursor"] is not None
    next_cursor = page1["next_cursor"]
    assert all("id" in r for r in page1["revisions"])

    second = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/paged/history?limit=10&cursor={next_cursor}",
        headers=auth_headers,
    )
    page2 = second.json()
    assert [r["revision_number"] for r in page2["revisions"]] == [3, 2, 1]
    # No more pages → next_cursor is None
    assert page2["next_cursor"] is None


@pytest.mark.asyncio
async def test_get_page(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test GET returns page with backlinks."""
    # Create page
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/overview",
        json={"content": "This is the overview.", "title": "Overview"},
        headers=auth_headers,
    )
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/overview",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "overview"
    assert data["content"] == "This is the overview."
    assert "backlinks" in data


@pytest.mark.asyncio
async def test_get_page_not_found(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test GET for non-existent page returns 404."""
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/nonexistent",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_page(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test DELETE removes a page."""
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/to-delete",
        json={"content": "Temporary.", "title": "To Delete"},
        headers=auth_headers,
    )
    resp = await client.delete(
        f"/api/v1/projects/{test_project.id}/pages/to-delete",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Confirm gone
    resp2 = await client.get(
        f"/api/v1/projects/{test_project.id}/pages/to-delete",
        headers=auth_headers,
    )
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_page_not_found(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test DELETE for non-existent page returns 404."""
    resp = await client.delete(
        f"/api/v1/projects/{test_project.id}/pages/no-such-page",
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pages(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test GET list returns all pages."""
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/page-a",
        json={"content": "Content A.", "title": "Page A"},
        headers=auth_headers,
    )
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/page-b",
        json={"content": "Content B.", "title": "Page B"},
        headers=auth_headers,
    )
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/pages",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    slugs = {p["slug"] for p in data}
    assert slugs == {"page-a", "page-b"}


@pytest.mark.asyncio
async def test_list_pages_empty(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test GET list with no pages returns empty list."""
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/pages",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Auto-narrative column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_narrative_column_exists(
    db_session: AsyncSession, test_user: User,
):
    """Test that Project model has auto_narrative field defaulting to False."""
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Narrative Test",
        git_remote_normalized="github.com/example/narrative-repo",
        context_document="",
        owner_id=test_user.id,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    assert project.auto_narrative is False


@pytest.mark.asyncio
async def test_update_project_settings(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test PUT settings updates auto_narrative."""
    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/settings",
        json={"auto_narrative": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_narrative"] is True


@pytest.mark.asyncio
async def test_regenerate_auto_generated_page(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test POST regenerate on auto_generated page succeeds."""
    # Create an auto-generated page directly in DB
    page = KnowledgePage(
        id=f"page_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        slug="concept/test-topic",
        title="Test Topic",
        page_type="concept",
        content="Old auto-generated content.",
        word_count=4,
        entry_count=0,
        auto_generated=True,
    )
    db_session.add(page)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/pages/concept/test-topic/regenerate",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "regenerated"
    assert "word_count" in data
    assert "entries_used" in data


@pytest.mark.asyncio
async def test_regenerate_non_auto_generated_page_returns_400(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test POST regenerate on non-auto_generated page returns 400."""
    # Create a normal (not auto-generated) page
    await client.put(
        f"/api/v1/projects/{test_project.id}/pages/manual-page",
        json={"content": "Manual content.", "title": "Manual Page"},
        headers=auth_headers,
    )
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/pages/manual-page/regenerate",
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "auto-generated" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_regenerate_nonexistent_page_returns_404(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    """Test POST regenerate on non-existent page returns 404."""
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/pages/no-such-page/regenerate",
        headers=auth_headers,
    )
    assert resp.status_code == 404
