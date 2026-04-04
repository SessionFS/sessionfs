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
    assert data["confidence"] == 0.95
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
